"""Backward-compatible amount helpers (default asset = PDC)."""

from __future__ import annotations

from funding.assets import (
    ASSET_PDC,
    ASSETS,
    format_amount as format_amount_asset,
    funding_pct,
    parse_amount as parse_amount_asset,
)

ATOMIC = ASSETS[ASSET_PDC].atomic
MILLI = 10**9


def parse_amount(text: str) -> int:
    return parse_amount_asset(text, ASSET_PDC)


def format_amount(atomic: int) -> str:
    return format_amount_asset(atomic, ASSET_PDC)


__all__ = [
    "ATOMIC",
    "MILLI",
    "parse_amount",
    "format_amount",
    "funding_pct",
    "format_amount_asset",
    "parse_amount_asset",
]
