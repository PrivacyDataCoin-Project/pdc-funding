"""Atomic amount helpers (1 PDC = 10^12). Display unit is 0.001 PDC."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN

ATOMIC = 10**12
# UI / operator amounts are shown to milli-PDC (0.001).
MILLI = 10**9
# SQLite INTEGER is signed 64-bit. Larger values raise OverflowError at insert.
_MAX_ATOMIC = 2**63 - 1
_MILLI_QUANT = Decimal("0.001")


def parse_amount(text: str) -> int:
    """Parse human PDC amount to atomic units. Raises ValueError on bad input."""
    raw = (text or "").strip().replace(",", ".")
    if not raw:
        raise ValueError("empty amount")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {text!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be positive")
    atomic = (value * ATOMIC).to_integral_value(rounding=ROUND_DOWN)
    if atomic <= 0:
        raise ValueError("amount too small")
    if atomic > _MAX_ATOMIC:
        raise ValueError("amount too large")
    return int(atomic)


def format_amount(atomic: int) -> str:
    """Format atomic units as PDC with 0.001 precision (floor to milli)."""
    if atomic < 0:
        raise ValueError("negative amount")
    milli = (int(atomic) // MILLI) * MILLI
    value = (Decimal(milli) / Decimal(ATOMIC)).quantize(
        _MILLI_QUANT, rounding=ROUND_DOWN
    )
    return f"{value:.3f}"


def funding_pct(received_atomic: int, target_atomic: int) -> float:
    """Percent of target funded (capped display helpers use this)."""
    if target_atomic <= 0:
        return 0.0
    if received_atomic <= 0:
        return 0.0
    return min(100.0, (received_atomic * 100.0) / target_atomic)
