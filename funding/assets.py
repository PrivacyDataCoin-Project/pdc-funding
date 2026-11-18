"""Supported funding assets: PDC (L1 native), BTCX (L1 confidential), BTC (Bitcoin)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Iterable, Optional

# SQLite INTEGER is signed 64-bit.
_MAX_ATOMIC = 2**63 - 1

# BTCX confidential asset on PDC L1 (explorer / Trade whitelist).
DEFAULT_BTCX_ASSET_ID = (
    "c2bf0410873b49ec7e7dd19d62bca9625d80cd3123670a67b48d997d3127426d"
)
DEFAULT_PDC_ASSET_ID = (
    "d6329b5b1f7c0805b5c345f4957554002a2f557845f64d7645dae0e051a6498a"
)

ASSET_PDC = "PDC"
ASSET_BTCX = "BTCX"
ASSET_BTC = "BTC"
SUPPORTED_ASSETS = (ASSET_PDC, ASSET_BTCX, ASSET_BTC)


@dataclass(frozen=True)
class AssetSpec:
    ticker: str
    decimals: int
    chain: str  # "pdc" | "btc"
    default_asset_id: str = ""
    display_places: int = 3

    @property
    def atomic(self) -> int:
        return 10**self.decimals


ASSETS: dict[str, AssetSpec] = {
    ASSET_PDC: AssetSpec(
        ticker=ASSET_PDC,
        decimals=12,
        chain="pdc",
        default_asset_id=DEFAULT_PDC_ASSET_ID,
        display_places=3,
    ),
    ASSET_BTCX: AssetSpec(
        ticker=ASSET_BTCX,
        decimals=8,
        chain="pdc",
        default_asset_id=DEFAULT_BTCX_ASSET_ID,
        display_places=8,
    ),
    ASSET_BTC: AssetSpec(
        ticker=ASSET_BTC,
        decimals=8,
        chain="btc",
        default_asset_id="",
        display_places=8,
    ),
}


def normalize_asset(raw: str) -> str:
    ticker = (raw or "").strip().upper()
    if ticker not in ASSETS:
        raise ValueError(f"unsupported asset: {raw!r}")
    return ticker


def parse_assets_csv(raw: str | Iterable[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    else:
        parts = [str(p).strip() for p in raw]
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        ticker = normalize_asset(part)
        if ticker not in out:
            out.append(ticker)
    return out


def parse_amount(text: str, asset: str = ASSET_PDC) -> int:
    spec = ASSETS[normalize_asset(asset)]
    raw = (text or "").strip().replace(",", ".")
    if not raw:
        raise ValueError("empty amount")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {text!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be positive")
    atomic = (value * spec.atomic).to_integral_value(rounding=ROUND_DOWN)
    if atomic <= 0:
        raise ValueError("amount too small")
    if atomic > _MAX_ATOMIC:
        raise ValueError("amount too large")
    return int(atomic)


def format_amount(atomic: int, asset: str = ASSET_PDC) -> str:
    if atomic < 0:
        raise ValueError("negative amount")
    spec = ASSETS[normalize_asset(asset)]
    places = spec.display_places
    step = Decimal(1) / (Decimal(10) ** places)
    # Floor to display places.
    quant_atomic = (10**spec.decimals) // (10**places)
    floored = (int(atomic) // quant_atomic) * quant_atomic
    value = (Decimal(floored) / Decimal(spec.atomic)).quantize(
        step, rounding=ROUND_DOWN
    )
    return f"{value:.{places}f}"


def funding_pct(received_atomic: int, target_atomic: int) -> float:
    if target_atomic <= 0 or received_atomic <= 0:
        return 0.0
    return min(100.0, (received_atomic * 100.0) / target_atomic)


def asset_id_for(settings, asset: str) -> str:
    ticker = normalize_asset(asset)
    if ticker == ASSET_PDC:
        return (getattr(settings, "pdc_asset_id", "") or DEFAULT_PDC_ASSET_ID).strip().lower()
    if ticker == ASSET_BTCX:
        return (
            getattr(settings, "btcx_asset_id", "") or DEFAULT_BTCX_ASSET_ID
        ).strip().lower()
    return ""


def ticker_for_asset_id(settings, asset_id: str) -> Optional[str]:
    """Map chain asset_id → ticker. Empty asset_id returns None (ambiguous)."""
    aid = (asset_id or "").strip().lower()
    if not aid:
        return None
    if aid == asset_id_for(settings, ASSET_PDC):
        return ASSET_PDC
    if aid == asset_id_for(settings, ASSET_BTCX):
        return ASSET_BTCX
    return None


def resolve_payment_ticker(
    settings,
    asset_id: str,
    *,
    proposal_tickers: set[str],
) -> Optional[str]:
    """Resolve deposit ticker for a payment against the proposal's selected assets."""
    aid = (asset_id or "").strip().lower()
    if not aid:
        # Legacy native payments omit asset_id. Only safe when the proposal is
        # PDC-only on L1 (no BTCX sharing the same payment_id).
        if ASSET_PDC in proposal_tickers and ASSET_BTCX not in proposal_tickers:
            return ASSET_PDC
        return None
    ticker = ticker_for_asset_id(settings, aid)
    if ticker is None:
        return None
    if ticker not in proposal_tickers:
        return None
    return ticker
