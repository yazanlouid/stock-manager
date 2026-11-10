"""Input checks shared by the domain functions.

Quantities and money are numeric(14,3) (hard rule 4), so both must be Decimals
with at most three decimal places. A non-Decimal is a programming error, not a
bad request, so it raises TypeError; everything else raises DomainError.
"""

from decimal import Decimal, InvalidOperation

from app.domain.errors import DomainError

# numeric(14,3): three decimal places.
QUANTITY_STEP = Decimal("0.001")
ZERO = Decimal(0)


def check_quantity(quantity: Decimal, name: str = "quantity") -> None:
    """A quantity must be a finite, positive Decimal with at most three decimals."""
    _check_decimal(quantity, name)
    if quantity <= 0:
        raise DomainError(f"{name} must be positive, got {quantity}")
    _check_step(quantity, name)


def check_money(amount: Decimal, name: str = "amount") -> None:
    """Money must be a finite, non-negative Decimal with at most three decimals."""
    _check_decimal(amount, name)
    if amount < 0:
        raise DomainError(f"{name} must not be negative, got {amount}")
    _check_step(amount, name)


def _check_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal, not {type(value).__name__}")
    # Check finiteness first: Decimal("NaN") > 0 raises InvalidOperation, and
    # Decimal("Infinity") > 0 is True.
    if not value.is_finite():
        raise DomainError(f"{name} must be finite, got {value}")


def _check_step(value: Decimal, name: str) -> None:
    try:
        on_step = value == value.quantize(QUANTITY_STEP)
    except InvalidOperation:
        # quantize raises when the result needs more digits than the context
        # precision (28), far beyond numeric(14,3).
        on_step = False
    if not on_step:
        raise DomainError(f"{name} must have at most three decimal places, got {value}")
