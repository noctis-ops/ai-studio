"""Micro-USD money helpers. Money is an integer number of micro-USD everywhere
inside the system (1 USD = 1_000_000 micro-USD); floats never carry an amount.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MUSD_PER_USD = 1_000_000
_MUSD = Decimal(MUSD_PER_USD)
_ONE = Decimal(1)


def usd_to_musd(value: Decimal | str | float | int) -> int:
    """Convert a USD amount to micro-USD, rounding half-up to the nearest unit."""
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a valid USD amount: {value!r}") from exc
    if dec.is_nan() or dec.is_infinite():
        raise ValueError(f"not a finite USD amount: {value!r}")
    return int((dec * _MUSD).quantize(_ONE, rounding=ROUND_HALF_UP))


def musd_to_usd(musd: int) -> Decimal:
    if not isinstance(musd, int):
        raise TypeError(f"micro-USD must be int, got {type(musd).__name__}")
    return (Decimal(musd) / _MUSD).quantize(Decimal("0.000001"))


def fmt_usd(musd: int) -> str:
    return f"${musd_to_usd(musd):.6f}"


def add_musd(a: int, b: int) -> int:
    return a + b


def ceil_div_musd(numerator: Decimal, denominator: Decimal) -> int:
    """ceil(numerator / denominator) computed on Decimals only."""
    if denominator == 0:
        raise ZeroDivisionError("denominator must be non-zero")
    q = (numerator / denominator).to_integral_value(rounding="ROUND_CEILING")
    return int(q)
