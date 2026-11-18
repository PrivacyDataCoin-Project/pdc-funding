"""Cover L1 PDC network fees from a proposal's funded assets (oracle conversion)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
from typing import Optional

from funding.assets import ASSET_BTC, ASSET_BTCX, ASSET_PDC, ASSETS
from funding.prices import PriceBook


# Extra BTCX/BTC reserved vs oracle mid to absorb short-term price moves.
_SLIP_BPS = 200  # 2%


@dataclass(frozen=True)
class FeeCoverPlan:
    """How to fund `pdc_fee_atomic` from this proposal's escrow only."""

    pdc_from_escrow: int
    btcx_from_escrow: int
    btc_from_escrow: int
    pdc_fee_atomic: int
    rate_btcx_usd: Optional[float]
    rate_pdc_usd: Optional[float]
    rate_btc_usd: Optional[float]


def _ceil_div(n: Decimal, d: Decimal) -> int:
    if d <= 0:
        raise ValueError("invalid price")
    return int((n / d).to_integral_value(rounding=ROUND_UP))


def asset_atomic_for_pdc_fee(
    pdc_fee_atomic: int,
    *,
    from_asset: str,
    book: PriceBook,
    slip_bps: int = _SLIP_BPS,
) -> int:
    """How many from_asset atomics cover pdc_fee_atomic at oracle (+ slip)."""
    if pdc_fee_atomic <= 0:
        return 0
    pdc_usd = book.prices.get("PDC") or book.prices.get("PDCX")
    if not pdc_usd or pdc_usd <= 0:
        raise ValueError("PDC USD price unavailable for fee conversion")
    if from_asset == ASSET_BTCX:
        src_usd = book.prices.get("BTCX") or book.prices.get("BTC")
        src_spec = ASSETS[ASSET_BTCX]
    elif from_asset == ASSET_BTC:
        src_usd = book.prices.get("BTC") or book.prices.get("BTCX")
        src_spec = ASSETS[ASSET_BTC]
    else:
        raise ValueError(f"cannot convert fee from {from_asset}")
    if not src_usd or src_usd <= 0:
        raise ValueError(f"{from_asset} USD price unavailable for fee conversion")

    pdc_units = Decimal(pdc_fee_atomic) / Decimal(ASSETS[ASSET_PDC].atomic)
    fee_usd = pdc_units * Decimal(str(pdc_usd))
    src_units = fee_usd / Decimal(str(src_usd))
    # Apply slip: need more source asset.
    src_units *= Decimal(10_000 + int(slip_bps)) / Decimal(10_000)
    atomic = _ceil_div(src_units * Decimal(src_spec.atomic), Decimal(1))
    if atomic <= 0:
        atomic = 1
    return int(atomic)


def plan_pdc_fee_cover(
    pdc_fee_atomic: int,
    *,
    available_pdc: int,
    available_btcx: int,
    available_btc: int,
    book: Optional[PriceBook],
) -> FeeCoverPlan:
    """Prefer PDC escrow, then BTCX, then BTC — never more than available."""
    if pdc_fee_atomic <= 0:
        return FeeCoverPlan(0, 0, 0, 0, None, None, None)
    pdc_use = min(max(0, available_pdc), pdc_fee_atomic)
    short = pdc_fee_atomic - pdc_use
    btcx_use = 0
    btc_use = 0
    rate_btcx = rate_pdc = rate_btc = None
    if book:
        rate_pdc = book.prices.get("PDC") or book.prices.get("PDCX")
        rate_btcx = book.prices.get("BTCX") or book.prices.get("BTC")
        rate_btc = book.prices.get("BTC") or book.prices.get("BTCX")
    if short > 0:
        if book is None:
            raise ValueError(
                "PDC escrow short for L1 fee and Trade prices unavailable "
                "to convert from BTCX/BTC"
            )
        if available_btcx > 0:
            need = asset_atomic_for_pdc_fee(short, from_asset=ASSET_BTCX, book=book)
            if need > available_btcx:
                # Take all BTCX and recompute remaining short in PDC terms.
                btcx_use = available_btcx
                # Value of btcx_use in PDC atomic (conservative: no slip undo).
                pdc_usd = Decimal(str(rate_pdc))
                btcx_usd = Decimal(str(rate_btcx))
                btcx_units = Decimal(btcx_use) / Decimal(ASSETS[ASSET_BTCX].atomic)
                covered_usd = btcx_units * btcx_usd
                covered_pdc = int(
                    (covered_usd / pdc_usd * Decimal(ASSETS[ASSET_PDC].atomic)).to_integral_value(
                        rounding=ROUND_UP
                    )
                )
                short = max(0, short - covered_pdc)
            else:
                btcx_use = need
                short = 0
        if short > 0 and available_btc > 0:
            need = asset_atomic_for_pdc_fee(short, from_asset=ASSET_BTC, book=book)
            if need > available_btc:
                raise ValueError(
                    "insufficient proposal escrow to cover L1 PDC fee "
                    f"(need {pdc_fee_atomic} PDC atomic; "
                    f"PDC avail {available_pdc}, BTCX avail {available_btcx}, "
                    f"BTC avail {available_btc})"
                )
            btc_use = need
            short = 0
        if short > 0:
            raise ValueError(
                "insufficient proposal escrow to cover L1 PDC network fee "
                f"(still short {short} PDC atomic after conversion)"
            )
    return FeeCoverPlan(
        pdc_from_escrow=pdc_use,
        btcx_from_escrow=btcx_use,
        btc_from_escrow=btc_use,
        pdc_fee_atomic=pdc_fee_atomic,
        rate_btcx_usd=float(rate_btcx) if rate_btcx else None,
        rate_pdc_usd=float(rate_pdc) if rate_pdc else None,
        rate_btc_usd=float(rate_btc) if rate_btc else None,
    )
