"""Turning an amount and a split rule into exact per-person shares.

Everything here works in integer cents. Money is never divided as a float, and the
shares are guaranteed to add back up to the original amount — a split that loses or
invents a cent is a bug that shows up as a permanently wrong balance.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.core.exceptions import UnprocessableEntityError
from app.models.expense import SplitType

CENTS = Decimal("0.01")
HUNDRED = Decimal("100")


@dataclass(frozen=True, slots=True)
class SplitInput:
    """One participant plus the value they were given, if the rule needs one."""

    user_id: uuid.UUID
    value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ComputedSplit:
    user_id: uuid.UUID
    amount: Decimal
    percentage: Decimal | None = None


def to_cents(amount: Decimal) -> int:
    return int(amount.quantize(CENTS, rounding=ROUND_HALF_UP) * 100)


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENTS)


def _plain(value: Decimal) -> str:
    """Trim trailing zeros without letting Decimal fall back to 9E+1 notation."""
    trimmed = value.normalize()
    return f"{trimmed:f}"


def _field_error(message: str, field: str = "splits") -> UnprocessableEntityError:
    return UnprocessableEntityError(
        message,
        details=[{"field": field, "message": message, "type": "split_error"}],
    )


def compute_splits(
    amount: Decimal,
    split_type: SplitType,
    participants: list[SplitInput],
) -> list[ComputedSplit]:
    """Resolve `participants` into shares that sum exactly to `amount`."""
    if not participants:
        raise _field_error("An expense needs at least one participant.")

    seen: set[uuid.UUID] = set()
    for participant in participants:
        if participant.user_id in seen:
            raise _field_error("The same person cannot appear twice in a split.")
        seen.add(participant.user_id)

    total_cents = to_cents(amount)
    if total_cents <= 0:
        raise _field_error("The amount must be greater than zero.", field="amount")

    match split_type:
        case SplitType.EQUAL:
            return _split_equally(total_cents, participants)
        case SplitType.EXACT:
            return _split_exactly(total_cents, participants)
        case SplitType.PERCENTAGE:
            return _split_by_percentage(total_cents, participants)

    raise _field_error(f"Unsupported split type {split_type!r}.", field="split_type")


def _split_equally(total_cents: int, participants: list[SplitInput]) -> list[ComputedSplit]:
    count = len(participants)
    base, remainder = divmod(total_cents, count)

    # `remainder` cents cannot be divided evenly. Hand them out one each, ordered by
    # user id so the same expense always produces the same shares.
    order = sorted(range(count), key=lambda i: str(participants[i].user_id))
    extra = {order[i] for i in range(remainder)}

    return [
        ComputedSplit(
            user_id=participant.user_id,
            amount=from_cents(base + (1 if index in extra else 0)),
        )
        for index, participant in enumerate(participants)
    ]


def _split_exactly(total_cents: int, participants: list[SplitInput]) -> list[ComputedSplit]:
    if any(participant.value is None for participant in participants):
        raise _field_error("Every participant needs an amount for an exact split.")

    shares = [to_cents(participant.value) for participant in participants]  # type: ignore[arg-type]

    if any(share < 0 for share in shares):
        raise _field_error("Split amounts cannot be negative.")

    difference = sum(shares) - total_cents
    if difference != 0:
        raise _field_error(
            f"Split amounts add up to {from_cents(sum(shares))}, "
            f"but the expense is {from_cents(total_cents)}. "
            f"{'Remove' if difference > 0 else 'Add'} {from_cents(abs(difference))}."
        )

    return [
        ComputedSplit(user_id=participant.user_id, amount=from_cents(share))
        for participant, share in zip(participants, shares, strict=True)
    ]


def _split_by_percentage(total_cents: int, participants: list[SplitInput]) -> list[ComputedSplit]:
    if any(participant.value is None for participant in participants):
        raise _field_error("Every participant needs a percentage.")

    percentages = [Decimal(participant.value) for participant in participants]  # type: ignore[arg-type]

    if any(percentage < 0 for percentage in percentages):
        raise _field_error("Percentages cannot be negative.")

    total_percentage = sum(percentages, Decimal(0))
    if total_percentage != HUNDRED:
        raise _field_error(
            f"Percentages add up to {_plain(total_percentage)}%, and must add up to 100%."
        )

    shares = [
        int((Decimal(total_cents) * percentage / HUNDRED).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        for percentage in percentages
    ]

    # Rounding each share independently can miss the total by a cent or two. Settle
    # the difference against the largest shares, where it is least visible.
    difference = total_cents - sum(shares)
    if difference:
        step = 1 if difference > 0 else -1
        order = sorted(range(len(shares)), key=lambda i: (-shares[i], str(participants[i].user_id)))
        for offset in range(abs(difference)):
            shares[order[offset % len(order)]] += step

    return [
        ComputedSplit(
            user_id=participant.user_id,
            amount=from_cents(share),
            percentage=percentage,
        )
        for participant, share, percentage in zip(participants, shares, percentages, strict=True)
    ]
