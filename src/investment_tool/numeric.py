"""Exact-decimal policy for reported financial values.

Contract (docs/DESIGN.md section 10):
- Reported money, prices, quantities: `decimal.Decimal`, stored as canonical
  TEXT in SQLite. Binary floats are permitted only for derived analytics
  (returns, z-scores, betas) and must never round-trip back into reported
  values.
- Missing values stay None end-to-end. They are never coerced to zero.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


class DecimalPolicyError(ValueError):
    """A value violated the exact-decimal storage policy."""


def dec(value: str | int | Decimal | None) -> Decimal | None:
    """Parse a reported value into Decimal. None passes through (explicitly missing).

    Floats are rejected: a binary float has already lost exactness, so the
    caller must supply the source string instead.
    """
    if value is None:
        return None
    if isinstance(value, float):
        raise DecimalPolicyError(
            f"float rejected for reported value: {value!r}; pass the source string"
        )
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise DecimalPolicyError(f"unparseable decimal: {value!r}") from exc


def dec_text(value: Decimal | None) -> str | None:
    """Canonical TEXT form for storage. None stays None (SQL NULL)."""
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise DecimalPolicyError(f"dec_text expects Decimal, got {type(value).__name__}")
    return format(value, "f")


def dec_from_db(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)
