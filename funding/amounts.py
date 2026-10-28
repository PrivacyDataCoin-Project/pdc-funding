"""Atomic amount helpers (1 PDC = 10^12)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN

ATOMIC = 10**12


def parse_amount(text: str) -> int:
    """Parse human PDC amount to atomic units. Raises ValueError on bad input."""
    raw = (text or "").strip().replace(",", ".")
    if not raw:
        raise ValueError("empty amount")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {text!r}") from exc
    if value <= 0:
        raise ValueError("amount must be positive")
    atomic = (value * ATOMIC).to_integral_value(rounding=ROUND_DOWN)
    if atomic <= 0:
        raise ValueError("amount too small")
    return int(atomic)


def format_amount(atomic: int) -> str:
    """Format atomic units as a readable PDC string."""
    if atomic < 0:
        raise ValueError("negative amount")
    whole, frac = divmod(int(atomic), ATOMIC)
    if frac == 0:
        return str(whole)
    frac_str = f"{frac:012d}".rstrip("0")
    return f"{whole}.{frac_str}"


def funding_pct(received_atomic: int, target_atomic: int) -> float:
    """Percent of target funded (capped display helpers use this)."""
    if target_atomic <= 0:
        return 0.0
    if received_atomic <= 0:
        return 0.0
    return min(100.0, (received_atomic * 100.0) / target_atomic)
