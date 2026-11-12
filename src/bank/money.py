"""Money in integer minor units, with ISO 4217 exponents and banker's rounding."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

# ISO 4217 minor-unit exponents for the currencies the demo uses.
EXPONENTS: dict[str, int] = {
    "EUR": 2,
    "USD": 2,
    "GBP": 2,
    "SGD": 2,
    "JPY": 0,
    "KRW": 0,
    "BHD": 3,
    "KWD": 3,
}


def exponent(currency: str) -> int:
    try:
        return EXPONENTS[currency]
    except KeyError:
        raise ValueError(f"unsupported currency: {currency}") from None


def to_minor(amount: Decimal | str | int, currency: str) -> int:
    """Convert a major-unit amount to minor units. Floats are rejected."""
    if isinstance(amount, float):
        raise TypeError("use Decimal or str for money, not float")
    value = Decimal(amount)
    scaled = value.scaleb(exponent(currency)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return int(scaled)


def from_minor(minor: int, currency: str) -> Decimal:
    exp = exponent(currency)
    return Decimal(minor).scaleb(-exp).quantize(Decimal(1).scaleb(-exp))


@dataclass(frozen=True)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        exponent(self.currency)

    @classmethod
    def of(cls, amount: Decimal | str | int, currency: str) -> Money:
        return cls(to_minor(amount, currency), currency)

    def _same(self, other: Money) -> None:
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch: {self.currency} and {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.minor - other.minor, self.currency)

    def __str__(self) -> str:
        return f"{from_minor(self.minor, self.currency)} {self.currency}"
