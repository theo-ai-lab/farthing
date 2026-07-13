"""Exact money arithmetic.

Money is represented as an integer count of the currency's minor units (cents for
USD) plus an ISO-4217 currency code. Integers are exact; floats are banned from
this module and from every module that computes a dollar figure. Proration is the
only place division occurs, and it is done in :class:`decimal.Decimal` with an
explicit rounding mode, then collapsed back to integer minor units.

This is the foundation of the load-bearing invariant: no dollar figure ever
originates from a prompt, and every figure that does exist is computed here from
trusted inputs with a deterministic, auditable rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True, slots=True)
class Money:
    """An exact monetary amount in a single currency.

    ``minor_units`` is a non-negative integer (Farthing never represents negative
    money; a refund is a positive amount moved in the refund direction).
    """

    minor_units: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.minor_units, int) or isinstance(self.minor_units, bool):
            raise TypeError("minor_units must be an int (never a float)")
        if self.minor_units < 0:
            raise ValueError("Money is non-negative; use direction at the call site")
        if not (isinstance(self.currency, str) and len(self.currency) == 3):
            raise ValueError("currency must be a 3-letter ISO-4217 code")

    def _same_ccy(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def add(self, other: "Money") -> "Money":
        self._same_ccy(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def sub_clamped(self, other: "Money") -> "Money":
        """Subtract, clamped at zero. Used for 'remaining refundable balance'."""
        self._same_ccy(other)
        return Money(max(0, self.minor_units - other.minor_units), self.currency)

    def min(self, other: "Money") -> "Money":
        self._same_ccy(other)
        return self if self.minor_units <= other.minor_units else other

    def __le__(self, other: "Money") -> bool:  # type: ignore[override]
        self._same_ccy(other)
        return self.minor_units <= other.minor_units


def zero(currency: str) -> Money:
    return Money(0, currency)


# ISO-4217 minor-unit exponents that are not 2. Refunds and the human-facing
# approval preview MUST format by the real exponent, or a reviewer approving a
# "50.00 JPY" refund is actually looking at ¥5000 shown as ¥50.00.
_ZERO_DECIMAL = frozenset({
    "JPY", "KRW", "VND", "CLP", "PYG", "XAF", "XOF", "XPF", "BIF", "DJF",
    "GNF", "ISK", "KMF", "RWF", "UGX", "VUV", "XCD",
})
_THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


def minor_unit_exponent(currency: str) -> int:
    c = currency.upper()
    if c in _ZERO_DECIMAL:
        return 0
    if c in _THREE_DECIMAL:
        return 3
    return 2


def format_money(minor_units: int, currency: str) -> str:
    """Human string with the currency's real minor-unit precision, computed in
    Decimal (never a float). ``5000 JPY`` -> ``5000 JPY``; ``1234 BHD`` -> ``1.234 BHD``."""
    if isinstance(minor_units, bool) or not isinstance(minor_units, int):
        raise TypeError("minor_units must be an int")
    exp = minor_unit_exponent(currency)
    if exp == 0:
        return f"{minor_units} {currency.upper()}"
    scaled = (Decimal(minor_units) / (Decimal(10) ** exp)).quantize(Decimal(1).scaleb(-exp))
    return f"{scaled} {currency.upper()}"


def pct_of(amount: Money, fraction: Decimal) -> Money:
    """A percentage of an amount, rounded HALF_UP to the nearest minor unit.

    ``fraction`` is a :class:`Decimal` in ``[0, 1]``. Never a float — passing a
    float raises, so a caller cannot smuggle rounding error into a dollar figure.
    """
    if isinstance(fraction, float):
        raise TypeError("fraction must be Decimal, not float")
    if not (Decimal(0) <= fraction <= Decimal(1)):
        raise ValueError("fraction must be within [0, 1]")
    raw = (Decimal(amount.minor_units) * fraction).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return Money(int(raw), amount.currency)


def prorate(amount: Money, remaining: Decimal, total: Decimal) -> Money:
    """Proration: ``amount * remaining / total``, HALF_UP to a minor unit.

    Both ``remaining`` and ``total`` are Decimals (e.g. days). ``total`` must be
    positive; ``remaining`` is clamped to ``[0, total]`` so proration can never
    exceed the original amount.
    """
    for name, val in (("remaining", remaining), ("total", total)):
        if isinstance(val, float):
            raise TypeError(f"{name} must be Decimal, not float")
    if total <= 0:
        raise ValueError("total must be positive")
    clamped = max(Decimal(0), min(remaining, total))
    fraction = clamped / total
    return pct_of(amount, fraction)
